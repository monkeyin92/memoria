from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.digital_self.compiler import build_manifest
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfVersion,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
)
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAuditTarget,
    LegacyFence,
    LegacyGrantConflictError,
    LegacyManifestItemRef,
    LegacyNotFoundError,
    LegacyRelationshipSnapshot,
    RegisteredGranteeSnapshot,
)
from services.legacy.registry import LegacyRegistry, _grant_digest
from services.self_model.domain import RelationshipProfile

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def _snapshots() -> tuple[DigitalSelfVersion, RelationshipProfile]:
    relationship_entry = RelationshipProfileManifestEntry(
        profile_id="11111111-1111-4111-8111-111111111111",
        version_number=3,
        person_id="person-1",
        relationship_id="22222222-2222-4222-8222-222222222222",
        salutation="小梅",
        tone="warm",
        advice_style="listen-first",
        sharing_scope="family",
        boundaries=("不替代专业意见",),
        support_source_event_ids=("relationship-source",),
        counterexample_source_event_ids=(),
    )
    memory_entry = MemoryClaimManifestEntry(
        claim_id="memory-1",
        category="life_story",
        subject_key="owner",
        predicate="lived_in",
        value="杭州",
        confidence=0.95,
        sensitive_domain="family",
        extractor_version="v1",
        source_event_id="memory-source",
        valid_at=NOW.isoformat(),
    )
    manifest, _, manifest_sha256 = build_manifest(
        (memory_entry, relationship_entry),
        compiler_version="digital-self-compiler-v3",
        policy_version="digital-self-policy-v3",
        persona_version_id=None,
        parent_version_id=None,
    )
    version = DigitalSelfVersion(
        version_id="33333333-3333-4333-8333-333333333333",
        account_id="owner-a",
        version_number=7,
        status="frozen",
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        created_at=NOW,
    )
    relationship = RelationshipProfile(
        profile_id="11111111-1111-4111-8111-111111111111",
        account_id="owner-a",
        version_number=3,
        person_id="person-1",
        relationship_id="22222222-2222-4222-8222-222222222222",
        salutation="小梅",
        tone="warm",
        advice_style="listen-first",
        sharing_scope="family",
        boundaries=("不替代专业意见",),
        status="approved",
        unresolved_conflict=False,
        sources=(),
        owner_reviewed_at=NOW,
        step_up_verified=True,
        created_at=NOW,
    )
    return version, relationship


async def _issue(path: Path, *, expires_at: datetime | None = None) -> tuple[LegacyRegistry, object]:
    registry = LegacyRegistry.sqlite(path)
    version, relationship = _snapshots()
    grant = await registry.issue(
        owner_account_id="owner-a",
        grantee=RegisteredGranteeSnapshot(account_id="grantee-a", registered_at=NOW),
        version=version,
        relationship_profile=relationship,
        allowed_items=(
            LegacyManifestItemRef(kind="memory_claim", item_id="memory-1"),
        ),
        visibility="family",
        voice_allowed=True,
        expires_at=expires_at or NOW + timedelta(days=30),
        idempotency_key="issue-1",
        now=NOW,
    )
    return registry, grant


@pytest.mark.asyncio
async def test_sqlite_lifecycle_derives_state_and_preserves_issue_idempotency(
    tmp_path: Path,
) -> None:
    registry, grant = await _issue(tmp_path / "legacy.sqlite3")
    version, relationship = _snapshots()
    duplicate = await registry.issue(
        owner_account_id="owner-a",
        grantee=RegisteredGranteeSnapshot(account_id="grantee-a", registered_at=NOW),
        version=version,
        relationship_profile=relationship,
        allowed_items=(LegacyManifestItemRef("memory_claim", "memory-1"),),
        visibility="family",
        voice_allowed=True,
        expires_at=NOW + timedelta(days=30),
        idempotency_key="issue-1",
        now=NOW,
    )
    assert duplicate == grant
    assert grant.status_at(NOW) == "pending"  # type: ignore[attr-defined]

    active = await registry.activate(
        actor_account_id="owner-a",
        grant_id=grant.grant_id,  # type: ignore[attr-defined]
        expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,  # type: ignore[attr-defined]
        idempotency_key="activate-1",
        now=NOW + timedelta(minutes=1),
    )
    assert active.status_at(NOW + timedelta(minutes=1)) == "active"
    assert active.status_at(NOW + timedelta(days=31)) == "expired"

    revoked = await registry.revoke(
        actor_account_id="owner-a",
        grant_id=grant.grant_id,  # type: ignore[attr-defined]
        expected_grant_snapshot_sha256=active.grant_snapshot_sha256,
        idempotency_key="revoke-1",
        now=NOW + timedelta(minutes=2),
    )
    assert revoked.status_at(NOW + timedelta(minutes=2)) == "revoked"
    with pytest.raises(LegacyGrantConflictError):
        await registry.activate(
            actor_account_id="owner-a",
            grant_id=grant.grant_id,  # type: ignore[attr-defined]
            expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,  # type: ignore[attr-defined]
            idempotency_key="activate-stale",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_issue_rejects_unfrozen_mismatched_private_and_unknown_scope(tmp_path: Path) -> None:
    registry = LegacyRegistry.sqlite(tmp_path / "legacy.sqlite3")
    version, relationship = _snapshots()
    common = dict(
        owner_account_id="owner-a",
        grantee=RegisteredGranteeSnapshot(account_id="grantee-a", registered_at=NOW),
        relationship_profile=relationship,
        voice_allowed=False,
        expires_at=NOW + timedelta(days=1),
        now=NOW,
    )
    for bad_version, refs, visibility in (
        (replace(version, status="approved"), (LegacyManifestItemRef("memory_claim", "memory-1"),), "family"),
        (version, (LegacyManifestItemRef("memory_claim", "missing"),), "family"),
        (version, (LegacyManifestItemRef("unknown", "memory-1"),), "family"),
        (version, (LegacyManifestItemRef("memory_claim", "memory-1"),), "private"),
        (version, (LegacyManifestItemRef("memory_claim", "memory-1"),), "unknown"),
    ):
        with pytest.raises(ValueError):
            await registry.issue(
                version=bad_version,
                allowed_items=refs,  # type: ignore[arg-type]
                visibility=visibility,  # type: ignore[arg-type]
                idempotency_key=f"bad-{visibility}-{refs[0].kind}",
                **common,
            )

    with pytest.raises(ValueError):
        await registry.issue(
            version=version,
            allowed_items=(LegacyManifestItemRef("memory_claim", "memory-1"),),
            visibility="family",
            idempotency_key="same-actor",
            **{**common, "grantee": RegisteredGranteeSnapshot("owner-a", NOW)},
        )


@pytest.mark.asyncio
async def test_issue_rejects_private_or_unscoped_manifest_items_and_scope_escalation(
    tmp_path: Path,
) -> None:
    registry = LegacyRegistry.sqlite(tmp_path / "legacy.sqlite3")
    version, relationship = _snapshots()
    relationship_entry = next(
        entry
        for entry in version.manifest.entries
        if isinstance(entry, RelationshipProfileManifestEntry)
    )
    private_items = (
        (
            MemoryClaimManifestEntry(
                claim_id="private-memory",
                category="life_story",
                subject_key="owner",
                predicate="remembers",
                value="private",
                confidence=0.9,
                sensitive_domain="private",
                extractor_version="v1",
                source_event_id="private-memory-source",
                valid_at=NOW.isoformat(),
            ),
            LegacyManifestItemRef("memory_claim", "private-memory"),
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
            LegacyManifestItemRef("persona_trait", "private-persona"),
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
            LegacyManifestItemRef("cognitive_claim", "private-cognitive"),
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
            LegacyManifestItemRef("decision_case", "private-decision"),
        ),
    )
    for entry, item in private_items:
        manifest, _, digest = build_manifest(
            (entry, relationship_entry),
            compiler_version="digital-self-compiler-v3",
            policy_version="digital-self-policy-v3",
            persona_version_id=(
                entry.persona_version_id
                if isinstance(entry, PersonaTraitManifestEntry)
                else None
            ),
            parent_version_id=None,
        )
        with pytest.raises(ValueError, match="private, unknown, or over-visible"):
            await registry.issue(
                owner_account_id="owner-a",
                grantee=RegisteredGranteeSnapshot("grantee-a", NOW),
                version=replace(version, manifest=manifest, manifest_sha256=digest),
                relationship_profile=relationship,
                allowed_items=(item,),
                visibility="family",
                voice_allowed=False,
                expires_at=NOW + timedelta(days=1),
                idempotency_key=f"private-{item.kind}",
                now=NOW,
            )

    private_relationship = replace(relationship, sharing_scope="private")
    private_relationship_entry = replace(relationship_entry, sharing_scope="private")
    private_manifest, _, private_digest = build_manifest(
        (private_relationship_entry,),
        compiler_version="digital-self-compiler-v3",
        policy_version="digital-self-policy-v3",
        persona_version_id=None,
        parent_version_id=None,
    )
    with pytest.raises(ValueError, match="relationship scope"):
        await registry.issue(
            owner_account_id="owner-a",
            grantee=RegisteredGranteeSnapshot("grantee-a", NOW),
            version=replace(version, manifest=private_manifest, manifest_sha256=private_digest),
            relationship_profile=private_relationship,
            allowed_items=(
                LegacyManifestItemRef("relationship_profile", relationship.profile_id),
            ),
            visibility="family",
            voice_allowed=False,
            expires_at=NOW + timedelta(days=1),
            idempotency_key="private-relationship",
            now=NOW,
        )

    public_relationship = replace(relationship, sharing_scope="public")
    public_relationship_entry = replace(relationship_entry, sharing_scope="public")
    family_cognitive = CognitiveClaimManifestEntry(
        claim_id="family-cognitive",
        claim_type="belief",
        statement="family belief",
        context="family",
        confidence=0.9,
        sharing_scope="family",
        support_source_event_ids=("family-cognitive-source",),
        counterexample_source_event_ids=(),
    )
    public_manifest, _, public_digest = build_manifest(
        (family_cognitive, public_relationship_entry),
        compiler_version="digital-self-compiler-v3",
        policy_version="digital-self-policy-v3",
        persona_version_id=None,
        parent_version_id=None,
    )
    with pytest.raises(ValueError, match="over-visible"):
        await registry.issue(
            owner_account_id="owner-a",
            grantee=RegisteredGranteeSnapshot("grantee-a", NOW),
            version=replace(version, manifest=public_manifest, manifest_sha256=public_digest),
            relationship_profile=public_relationship,
            allowed_items=(LegacyManifestItemRef("cognitive_claim", "family-cognitive"),),
            visibility="public",
            voice_allowed=False,
            expires_at=NOW + timedelta(days=1),
            idempotency_key="over-visible-item",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_access_shell_turns_and_preferences_are_isolated(tmp_path: Path) -> None:
    registry, grant = await _issue(tmp_path / "legacy.sqlite3")
    active = await registry.activate(
        actor_account_id="owner-a",
        grant_id=grant.grant_id,  # type: ignore[attr-defined]
        expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,  # type: ignore[attr-defined]
        idempotency_key="activate-1",
        now=NOW,
    )
    preview = await registry.resolve_access(
        actor_account_id="owner-a",
        grant_id=active.grant_id,
        purpose="owner_preview",
        now=NOW,
    )
    assert preview.actor_role == "owner_preview"
    assert preview.shell_id is None
    assert preview.allowed_items == active.allowed_items

    access = await registry.resolve_access(
        actor_account_id="grantee-a",
        grant_id=active.grant_id,
        purpose="grantee_session",
        now=NOW,
    )
    duplicate = await registry.resolve_access(
        actor_account_id="grantee-a",
        grant_id=active.grant_id,
        purpose="grantee_session",
        now=NOW,
    )
    assert access.shell_id == duplicate.shell_id
    assert access.relationship_profile_version == 3
    assert access.resource_owner_account_id == "owner-a"

    turn = await registry.append_shell_turn(
        actor_account_id="grantee-a",
        shell_id=access.shell_id or "",
        actor_role="grantee",
        actual_heard_text="我今天想听你讲杭州。",
        fence=LegacyFence("session-1", "turn-1", "generation-1", 4),
        idempotency_key="turn-1",
        now=NOW,
    )
    assert turn.actual_heard_text == "我今天想听你讲杭州。"
    shell = await registry.update_shell_preferences(
        actor_account_id="grantee-a",
        shell_id=access.shell_id or "",
        preferences=(
            ("preferred_response_length", "brief"),
            ("question_frequency", "rare"),
        ),
        expected_shell_revision=1,
        idempotency_key="preference-1",
        now=NOW,
    )
    assert shell.preferences == (
        ("preferred_response_length", "brief"),
        ("question_frequency", "rare"),
    )

    with pytest.raises((LegacyNotFoundError, LegacyAccessDeniedError)):
        await registry.get_grant(actor_account_id="stranger", grant_id=active.grant_id)
    with pytest.raises((LegacyNotFoundError, LegacyAccessDeniedError)):
        await registry.get_shell(
            actor_account_id="stranger", shell_id=access.shell_id or "", now=NOW
        )


@pytest.mark.parametrize("state", ("revoked", "expired"))
@pytest.mark.asyncio
async def test_shell_preferences_require_an_active_grant(
    tmp_path: Path,
    state: str,
) -> None:
    expires_at = NOW + timedelta(minutes=2) if state == "expired" else None
    registry, grant = await _issue(tmp_path / f"legacy-{state}.sqlite3", expires_at=expires_at)
    active = await registry.activate(
        actor_account_id="owner-a",
        grant_id=grant.grant_id,  # type: ignore[attr-defined]
        expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,  # type: ignore[attr-defined]
        idempotency_key="activate-1",
        now=NOW + timedelta(minutes=1),
    )
    access = await registry.resolve_access(
        actor_account_id="grantee-a",
        grant_id=active.grant_id,
        purpose="grantee_session",
        now=NOW + timedelta(minutes=1),
    )
    preferences = (
        ("preferred_response_length", "brief"),
        ("question_frequency", "rare"),
    )
    await registry.update_shell_preferences(
        actor_account_id="grantee-a",
        shell_id=access.shell_id or "",
        preferences=preferences,
        expected_shell_revision=1,
        idempotency_key="preference-before-inactive",
        now=NOW + timedelta(minutes=1, seconds=30),
    )
    if state == "revoked":
        await registry.revoke(
            actor_account_id="owner-a",
            grant_id=active.grant_id,
            expected_grant_snapshot_sha256=active.grant_snapshot_sha256,
            idempotency_key="revoke-1",
            now=NOW + timedelta(minutes=2),
        )
    with pytest.raises(LegacyAccessDeniedError, match="not active"):
        await registry.update_shell_preferences(
            actor_account_id="grantee-a",
            shell_id=access.shell_id or "",
            preferences=preferences,
            expected_shell_revision=1,
            idempotency_key="preference-before-inactive",
            now=NOW + timedelta(minutes=3),
        )
    for actor_account_id in ("owner-a", "grantee-a"):
        with pytest.raises(LegacyAccessDeniedError, match="not active"):
            await registry.get_shell(
                actor_account_id=actor_account_id,
                shell_id=access.shell_id or "",
                now=NOW + timedelta(minutes=3),
            )
    with pytest.raises(LegacyNotFoundError):
        await registry.get_shell(
            actor_account_id="other-grantee",
            shell_id=access.shell_id or "",
            now=NOW + timedelta(minutes=3),
        )
    with pytest.raises(LegacyNotFoundError):
        await registry.get_shell_for_grant(
            actor_account_id="other-grantee",
            grant_id=active.grant_id,
            now=NOW + timedelta(minutes=3),
        )
        with pytest.raises(LegacyAccessDeniedError, match="not active"):
            await registry.get_shell_for_grant(
                actor_account_id=actor_account_id,
                grant_id=active.grant_id,
                now=NOW + timedelta(minutes=3),
            )


@pytest.mark.asyncio
async def test_audit_contract_contains_no_text_or_free_payload(tmp_path: Path) -> None:
    registry, grant = await _issue(tmp_path / "legacy.sqlite3")
    await registry.activate(
        actor_account_id="owner-a",
        grant_id=grant.grant_id,  # type: ignore[attr-defined]
        expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,  # type: ignore[attr-defined]
        idempotency_key="activate-1",
        now=NOW,
    )
    events = await registry.list_audit_events(actor_account_id="owner-a", grant_id=grant.grant_id)  # type: ignore[attr-defined]
    assert {event.action for event in events} == {"issue", "activate"}
    assert all(not hasattr(event, "payload") for event in events)
    assert all("杭州" not in repr(event) for event in events)


@pytest.mark.asyncio
async def test_runtime_audit_is_bounded_idempotent_and_exactly_scoped(tmp_path: Path) -> None:
    registry, grant = await _issue(tmp_path / "legacy.sqlite3")
    active = await registry.activate(
        actor_account_id="owner-a",
        grant_id=grant.grant_id,  # type: ignore[attr-defined]
        expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,  # type: ignore[attr-defined]
        idempotency_key="activate-1",
        now=NOW,
    )
    await registry.resolve_access(
        actor_account_id="grantee-a",
        grant_id=active.grant_id,
        purpose="grantee_session",
        now=NOW,
    )
    fence = LegacyFence("session-1", "turn-1", "generation-1", 4)
    event = await registry.append_runtime_audit(
        actor_account_id="grantee-a",
        grant_id=active.grant_id,
        action="read_source",
        decision="allowed",
        reason="source_read",
        fence=fence,
        target=LegacyAuditTarget("memory_claim", "memory-1"),
        now=NOW,
    )
    duplicate = await registry.append_runtime_audit(
        actor_account_id="grantee-a",
        grant_id=active.grant_id,
        action="read_source",
        decision="allowed",
        reason="source_read",
        fence=fence,
        target=LegacyAuditTarget("memory_claim", "memory-1"),
        now=NOW + timedelta(seconds=1),
    )

    assert duplicate == event
    assert event.owner_account_id == "owner-a"
    assert event.grantee_account_id == "grantee-a"
    assert event.shell_id is not None
    assert (event.session_id, event.turn_id, event.generation_id, event.tool_epoch) == (
        "session-1",
        "turn-1",
        "generation-1",
        4,
    )
    assert (event.target_kind, event.target_id) == ("memory_claim", "memory-1")
    planned = await registry.append_runtime_audit(
        actor_account_id="grantee-a",
        grant_id=active.grant_id,
        action="plan_answer",
        decision="allowed",
        reason="answer_planned",
        fence=fence,
        target=None,
        now=NOW,
    )
    delivered = await registry.append_runtime_audit(
        actor_account_id="grantee-a",
        grant_id=active.grant_id,
        action="plan_answer",
        decision="allowed",
        reason="answer_planned",
        fence=fence,
        target=None,
        now=NOW + timedelta(seconds=2),
    )
    assert delivered == planned
    with pytest.raises(LegacyNotFoundError):
        await registry.append_runtime_audit(
            actor_account_id="other-grantee",
            grant_id=active.grant_id,
            action="plan_answer",
            decision="allowed",
            reason="answer_planned",
            fence=fence,
            target=None,
            now=NOW,
        )
    events = await registry.list_audit_events(
        actor_account_id="owner-a", grant_id=active.grant_id
    )
    assert [item for item in events if item.action == "read_source"] == [event]
    assert [item for item in events if item.action == "plan_answer"] == [planned]
    assert all(not hasattr(item, "payload") for item in events)
    exported = await registry.export_for_account(account_id="grantee-a")
    assert event in exported.audit_events and planned in exported.audit_events
    await registry.delete_for_account(account_id="grantee-a")
    deleted = await registry.export_for_account(account_id="owner-a")
    assert not (deleted.grants or deleted.shells or deleted.shell_turns or deleted.audit_events)


@pytest.mark.asyncio
async def test_runtime_audit_rejects_invalid_reason_target_and_inactive_grant(
    tmp_path: Path,
) -> None:
    registry, grant = await _issue(tmp_path / "legacy.sqlite3")
    fence = LegacyFence("session-1", "turn-1", "generation-1", 0)
    voice = await registry.append_runtime_audit(
        actor_account_id="owner-a",
        grant_id=grant.grant_id,  # type: ignore[attr-defined]
        action="select_voice",
        decision="allowed",
        reason="voice_selected",
        fence=None,
        target=LegacyAuditTarget("voice_profile", "fallback-voice"),
        now=NOW,
    )
    assert voice.shell_id is None
    assert voice.session_id is None
    assert (voice.target_kind, voice.target_id) == ("voice_profile", "fallback-voice")
    with pytest.raises(ValueError, match="action, decision, and reason"):
        await registry.append_runtime_audit(
            actor_account_id="owner-a",
            grant_id=grant.grant_id,  # type: ignore[attr-defined]
            action="plan_answer",
            decision="denied",
            reason="answer_planned",
            fence=fence,
            target=None,
            now=NOW,
        )
    with pytest.raises(ValueError, match="manifest item"):
        await registry.append_runtime_audit(
            actor_account_id="owner-a",
            grant_id=grant.grant_id,  # type: ignore[attr-defined]
            action="read_source",
            decision="allowed",
            reason="source_read",
            fence=fence,
            target=LegacyAuditTarget("voice_profile", "voice-1"),
            now=NOW,
        )
    with pytest.raises(LegacyAccessDeniedError):
        await registry.append_runtime_audit(
            actor_account_id="grantee-a",
            grant_id=grant.grant_id,  # type: ignore[attr-defined]
            action="plan_answer",
            decision="allowed",
            reason="answer_planned",
            fence=fence,
            target=None,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_account_export_and_delete_cover_owner_and_grantee_links(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    registry, grant = await _issue(path)
    assert (await registry.export_for_account(account_id="grantee-a")).grants == (grant,)
    await registry.delete_for_account(account_id="grantee-a")
    assert (await registry.export_for_account(account_id="owner-a")).grants == ()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM legacy_command_receipts"
        ).fetchone()[0] == 0


def test_grant_snapshot_digest_binds_the_exact_scope() -> None:
    version, relationship = _snapshots()
    shared = {
        "grant_id": "grant-1",
        "owner_account_id": "owner-a",
        "grantee_account_id": "grantee-a",
        "version_id": version.version_id,
        "version_number": version.version_number,
        "manifest_sha256": version.manifest_sha256,
        "relationship_profile_id": relationship.profile_id,
        "relationship_profile_version": relationship.version_number,
        "relationship": LegacyRelationshipSnapshot(
            profile_id=relationship.profile_id,
            version_number=relationship.version_number,
            relationship_id=relationship.relationship_id,
            salutation=relationship.salutation,
            tone=relationship.tone,
            advice_style=relationship.advice_style,
            sharing_scope=relationship.sharing_scope,
            boundaries=relationship.boundaries,
        ),
        "voice_allowed": False,
        "expires_at": NOW + timedelta(days=30),
        "activated_at": None,
        "revoked_at": None,
        "revision": 1,
        "created_at": NOW,
    }

    assert _grant_digest(scope_sha256="a" * 64, **shared) != _grant_digest(
        scope_sha256="b" * 64,
        **shared,
    )
