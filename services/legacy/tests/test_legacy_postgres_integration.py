from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.digital_self.compiler import build_manifest
from services.digital_self.domain import MemoryClaimManifestEntry
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAccessPurpose,
    LegacyAuditTarget,
    LegacyFence,
    LegacyGrant,
    LegacyGrantConflictError,
    LegacyManifestItemRef,
    LegacyNotFoundError,
    RegisteredGranteeSnapshot,
)
from services.legacy.postgres_registry import PostgresLegacyRegistry
from services.legacy.tests.test_legacy_registry import NOW, _snapshots


def _postgres_dsn(dsn: str, *, database: str, user: str | None = None, password: str = "") -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    netloc = parsed.netloc
    if user is not None:
        netloc = f"{quote(user)}:{quote(password)}@{host}"
    return urlunsplit((parsed.scheme, netloc, f"/{database}", parsed.query, ""))


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _expected_grant_digest(grant: LegacyGrant) -> str:
    return _digest(
        {
            "grant_id": grant.grant_id,
            "owner_account_id": grant.owner_account_id,
            "grantee_account_id": grant.grantee_account_id,
            "version_id": grant.version_id,
            "version_number": grant.version_number,
            "manifest_sha256": grant.manifest_sha256,
            "relationship_profile_id": grant.relationship.profile_id,
            "relationship_profile_version": grant.relationship.version_number,
            "relationship": {
                "relationship_id": grant.relationship.relationship_id,
                "salutation": grant.relationship.salutation,
                "tone": grant.relationship.tone,
                "advice_style": grant.relationship.advice_style,
                "sharing_scope": grant.relationship.sharing_scope,
                "boundaries": list(grant.relationship.boundaries),
            },
            "scope_sha256": grant.scope_sha256,
            "voice_allowed": grant.voice_allowed,
            "expires_at": grant.expires_at.isoformat(),
            "activated_at": grant.activated_at.isoformat() if grant.activated_at else None,
            "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
            "revision": grant.revision,
            "created_at": grant.created_at.isoformat(),
        }
    )


async def _issue(
    registry: PostgresLegacyRegistry,
    *,
    owner_account_id: str,
    grantee_account_id: str,
    idempotency_key: str,
    expires_at: datetime = NOW + timedelta(days=30),
    allowed_items: tuple[LegacyManifestItemRef, ...] | None = None,
) -> LegacyGrant:
    version, relationship = _snapshots()
    return await registry.issue(
        owner_account_id=owner_account_id,
        grantee=RegisteredGranteeSnapshot(grantee_account_id, NOW),
        version=version,
        relationship_profile=relationship,
        allowed_items=allowed_items or (LegacyManifestItemRef("memory_claim", "memory-1"),),
        visibility="family",
        voice_allowed=True,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
        now=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL Legacy integration contract",
)
async def test_postgres_legacy_registry_full_lifecycle_rls_export_and_delete() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex[:10]
    database = f"memoria_legacy_integration_{suffix}"
    app_role = f"memoria_legacy_integration_{suffix}"
    password = f"legacy-integration-{suffix}-password"
    owner = "owner-a"
    grantee = "grantee-a"
    second_grantee = "grantee-b"
    stranger = "stranger"
    admin = await asyncpg.connect(admin_dsn)
    database_admin: asyncpg.Connection | None = None
    registry: PostgresLegacyRegistry | None = None
    try:
        await admin.execute(
            f"CREATE ROLE \"{app_role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"
        )
        await admin.execute(f'CREATE DATABASE "{database}" OWNER "{app_role}"')
        app_dsn = _postgres_dsn(
            admin_dsn,
            database=database,
            user=app_role,
            password=password,
        )
        database_admin = await asyncpg.connect(_postgres_dsn(admin_dsn, database=database))
        await database_admin.execute(
            """
            CREATE TABLE mock_owner_core (
                account_id TEXT PRIMARY KEY,
                marker TEXT NOT NULL
            )
            """
        )
        await database_admin.executemany(
            "INSERT INTO mock_owner_core(account_id,marker) VALUES ($1,'must-survive')",
            [(owner,), (grantee,), (second_grantee,)],
        )

        registry = PostgresLegacyRegistry(app_dsn)
        await registry.initialize()

        version, relationship = _snapshots()
        relationship_entry = next(
            entry
            for entry in version.manifest.entries
            if entry.entry_type == "relationship_profile"
        )
        memory_entry = next(
            entry
            for entry in version.manifest.entries
            if isinstance(entry, MemoryClaimManifestEntry)
        )
        private_manifest, _, private_digest = build_manifest(
            (replace(memory_entry, sensitive_domain="private"), relationship_entry),
            compiler_version="digital-self-compiler-v3",
            policy_version="digital-self-policy-v3",
            persona_version_id=None,
            parent_version_id=None,
        )
        with pytest.raises(ValueError, match="private, unknown, or over-visible"):
            await registry.issue(
                owner_account_id=owner,
                grantee=RegisteredGranteeSnapshot(grantee, NOW),
                version=replace(
                    version,
                    manifest=private_manifest,
                    manifest_sha256=private_digest,
                ),
                relationship_profile=relationship,
                allowed_items=(LegacyManifestItemRef("memory_claim", memory_entry.claim_id),),
                visibility="family",
                voice_allowed=False,
                expires_at=NOW + timedelta(days=1),
                idempotency_key="private-memory-issue",
                now=NOW,
            )
        requested_items = (
            LegacyManifestItemRef("relationship_profile", relationship.profile_id),
            LegacyManifestItemRef("memory_claim", "memory-1"),
        )
        main_grant = await _issue(
            registry,
            owner_account_id=owner,
            grantee_account_id=grantee,
            idempotency_key="main-issue",
            allowed_items=requested_items,
        )
        duplicate_issue = await _issue(
            registry,
            owner_account_id=owner,
            grantee_account_id=grantee,
            idempotency_key="main-issue",
            allowed_items=requested_items,
        )
        expected_items = tuple(sorted(requested_items, key=lambda item: (item.kind, item.item_id)))
        expected_scope = _digest(
            {
                "visibility": "family",
                "allowed_items": [
                    {"kind": item.kind, "item_id": item.item_id} for item in expected_items
                ],
            }
        )
        assert duplicate_issue == main_grant
        assert main_grant.allowed_items == expected_items
        assert main_grant.scope_sha256 == expected_scope
        assert main_grant.grant_snapshot_sha256 == _expected_grant_digest(main_grant)

        active = await registry.activate(
            actor_account_id=owner,
            grant_id=main_grant.grant_id,
            expected_grant_snapshot_sha256=main_grant.grant_snapshot_sha256,
            idempotency_key="main-activate",
            now=NOW + timedelta(minutes=1),
        )
        duplicate_activate = await registry.activate(
            actor_account_id=owner,
            grant_id=main_grant.grant_id,
            expected_grant_snapshot_sha256=main_grant.grant_snapshot_sha256,
            idempotency_key="main-activate",
            now=NOW + timedelta(minutes=1),
        )
        assert duplicate_activate == active
        assert active.grant_snapshot_sha256 == _expected_grant_digest(active)
        with pytest.raises(LegacyGrantConflictError):
            await registry.activate(
                actor_account_id=owner,
                grant_id=active.grant_id,
                expected_grant_snapshot_sha256=main_grant.grant_snapshot_sha256,
                idempotency_key="main-activate-stale",
                now=NOW + timedelta(minutes=2),
            )

        preview = await registry.resolve_access(
            actor_account_id=owner,
            grant_id=active.grant_id,
            purpose="owner_preview",
            now=NOW + timedelta(minutes=2),
        )
        assert preview.actor_role == "owner_preview"
        assert preview.shell_id is None
        assert preview.allowed_items == expected_items
        access = await registry.resolve_access(
            actor_account_id=grantee,
            grant_id=active.grant_id,
            purpose="grantee_session",
            now=NOW + timedelta(minutes=2),
        )
        shell_id = access.shell_id
        assert access.actor_role == "grantee"
        assert access.resource_owner_account_id == owner
        assert access.allowed_items == expected_items
        assert shell_id is not None
        shell = await registry.get_shell(actor_account_id=grantee, shell_id=shell_id, now=NOW)
        assert shell.preferences == (
            ("preferred_response_length", "balanced"),
            ("question_frequency", "occasional"),
        )
        assert shell.revision == 1

        for account_id in (second_grantee, stranger):
            with pytest.raises(LegacyNotFoundError):
                await registry.get_grant(actor_account_id=account_id, grant_id=active.grant_id)
            with pytest.raises(LegacyNotFoundError):
                await registry.get_shell(actor_account_id=account_id, shell_id=shell_id, now=NOW)
            with pytest.raises(LegacyNotFoundError):
                await registry.resolve_access(
                    actor_account_id=account_id,
                    grant_id=active.grant_id,
                    purpose="grantee_session",
                    now=NOW + timedelta(minutes=2),
                )

        app_connection = await asyncpg.connect(app_dsn)
        try:
            assert await app_connection.fetchval(
                "SELECT NOT rolbypassrls FROM pg_roles WHERE rolname=current_user"
            )
            rls = await app_connection.fetch(
                """
                SELECT relname,relrowsecurity,relforcerowsecurity
                FROM pg_class
                WHERE relname=ANY($1::text[])
                ORDER BY relname
                """,
                [
                    "legacy_audit_events",
                    "legacy_command_receipts",
                    "legacy_grants",
                    "legacy_relationship_shells",
                    "legacy_shell_turns",
                ],
            )
            assert len(rls) == 5
            assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rls)
            async with app_connection.transaction():
                await app_connection.execute(
                    "SELECT set_config('app.actor_account_id',$1,true)", owner
                )
                assert await app_connection.fetchval("SELECT count(*) FROM legacy_grants") == 1
                assert (
                    await app_connection.fetchval("SELECT count(*) FROM legacy_relationship_shells")
                    == 1
                )
            async with app_connection.transaction():
                await app_connection.execute(
                    "SELECT set_config('app.actor_account_id',$1,true)", stranger
                )
                assert await app_connection.fetchval("SELECT count(*) FROM legacy_grants") == 0
                assert (
                    await app_connection.fetchval("SELECT count(*) FROM legacy_relationship_shells")
                    == 0
                )
                assert (
                    await app_connection.fetchval("SELECT count(*) FROM legacy_audit_events") == 0
                )
                assert (
                    await app_connection.fetchval("SELECT count(*) FROM legacy_command_receipts")
                    == 0
                )
        finally:
            await app_connection.close()

        fence = LegacyFence("session-1", "turn-1", "generation-1", 4)
        turn = await registry.append_shell_turn(
            actor_account_id=grantee,
            shell_id=shell_id,
            actor_role="grantee",
            actual_heard_text="我实际听到的是杭州的故事。",
            fence=fence,
            idempotency_key="main-turn",
            now=NOW + timedelta(minutes=3),
        )
        duplicate_turn = await registry.append_shell_turn(
            actor_account_id=grantee,
            shell_id=shell_id,
            actor_role="grantee",
            actual_heard_text="我实际听到的是杭州的故事。",
            fence=fence,
            idempotency_key="main-turn",
            now=NOW + timedelta(minutes=3),
        )
        assert duplicate_turn == turn
        assert turn.actual_heard_text == "我实际听到的是杭州的故事。"
        runtime_audit = await registry.append_runtime_audit(
            actor_account_id=grantee,
            grant_id=active.grant_id,
            action="read_source",
            decision="allowed",
            reason="source_read",
            fence=fence,
            target=LegacyAuditTarget("memory_claim", "memory-1"),
            now=NOW + timedelta(minutes=3),
        )
        duplicate_runtime_audit = await registry.append_runtime_audit(
            actor_account_id=grantee,
            grant_id=active.grant_id,
            action="read_source",
            decision="allowed",
            reason="source_read",
            fence=fence,
            target=LegacyAuditTarget("memory_claim", "memory-1"),
            now=NOW + timedelta(minutes=3, seconds=1),
        )
        assert duplicate_runtime_audit == runtime_audit
        assert runtime_audit.shell_id == shell_id
        assert runtime_audit.target_kind == "memory_claim"
        with pytest.raises(LegacyNotFoundError):
            await registry.append_runtime_audit(
                actor_account_id=second_grantee,
                grant_id=active.grant_id,
                action="plan_answer",
                decision="allowed",
                reason="answer_planned",
                fence=fence,
                target=None,
                now=NOW + timedelta(minutes=3),
            )
        preferences = (
            ("question_frequency", "rare"),
            ("preferred_response_length", "brief"),
        )
        updated_shell = await registry.update_shell_preferences(
            actor_account_id=grantee,
            shell_id=shell_id,
            preferences=preferences,
            expected_shell_revision=1,
            idempotency_key="main-preferences",
            now=NOW + timedelta(minutes=4),
        )
        duplicate_preferences = await registry.update_shell_preferences(
            actor_account_id=grantee,
            shell_id=shell_id,
            preferences=preferences,
            expected_shell_revision=1,
            idempotency_key="main-preferences",
            now=NOW + timedelta(minutes=4),
        )
        assert duplicate_preferences == updated_shell
        assert updated_shell.preferences == tuple(sorted(preferences))
        assert updated_shell.revision == 2
        with pytest.raises(LegacyGrantConflictError):
            await registry.update_shell_preferences(
                actor_account_id=grantee,
                shell_id=shell_id,
                preferences=preferences,
                expected_shell_revision=1,
                idempotency_key="main-preferences-stale",
                now=NOW + timedelta(minutes=5),
            )

        owner_export = await registry.export_for_account(account_id=owner)
        grantee_export = await registry.export_for_account(account_id=grantee)
        for exported in (owner_export, grantee_export):
            assert exported.grants == (active,)
            assert exported.shells == (updated_shell,)
            assert exported.shell_turns == (turn,)
            assert {event.action for event in exported.audit_events} == {
                "issue",
                "activate",
                "resolve_access",
                "append_shell_turn",
                "read_source",
                "update_shell_preferences",
            }

        expired = await _issue(
            registry,
            owner_account_id=owner,
            grantee_account_id=grantee,
            idempotency_key="expired-issue",
            expires_at=NOW + timedelta(minutes=2),
        )
        expired = await registry.activate(
            actor_account_id=owner,
            grant_id=expired.grant_id,
            expected_grant_snapshot_sha256=expired.grant_snapshot_sha256,
            idempotency_key="expired-activate",
            now=NOW + timedelta(minutes=1),
        )
        expired_access = await registry.resolve_access(
            actor_account_id=grantee,
            grant_id=expired.grant_id,
            purpose="grantee_session",
            now=NOW + timedelta(minutes=1, seconds=30),
        )
        inactive_preferences = (
            ("preferred_response_length", "brief"),
            ("question_frequency", "rare"),
        )
        await registry.update_shell_preferences(
            actor_account_id=grantee,
            shell_id=expired_access.shell_id or "",
            preferences=inactive_preferences,
            expected_shell_revision=1,
            idempotency_key=f"inactive-preferences-{expired.grant_id}",
            now=NOW + timedelta(minutes=1, seconds=30),
        )
        revoked = await _issue(
            registry,
            owner_account_id=owner,
            grantee_account_id=grantee,
            idempotency_key="revoked-issue",
        )
        revoked = await registry.activate(
            actor_account_id=owner,
            grant_id=revoked.grant_id,
            expected_grant_snapshot_sha256=revoked.grant_snapshot_sha256,
            idempotency_key="revoked-activate",
            now=NOW + timedelta(minutes=1),
        )
        revoked_access = await registry.resolve_access(
            actor_account_id=grantee,
            grant_id=revoked.grant_id,
            purpose="grantee_session",
            now=NOW + timedelta(minutes=1, seconds=30),
        )
        await registry.update_shell_preferences(
            actor_account_id=grantee,
            shell_id=revoked_access.shell_id or "",
            preferences=inactive_preferences,
            expected_shell_revision=1,
            idempotency_key=f"inactive-preferences-{revoked.grant_id}",
            now=NOW + timedelta(minutes=1, seconds=30),
        )
        revoked = await registry.revoke(
            actor_account_id=owner,
            grant_id=revoked.grant_id,
            expected_grant_snapshot_sha256=revoked.grant_snapshot_sha256,
            idempotency_key="revoked-revoke",
            now=NOW + timedelta(minutes=2),
        )
        for denied_grant, denied_at in (
            (expired, NOW + timedelta(minutes=3)),
            (revoked, NOW + timedelta(minutes=3)),
        ):
            denied_requests: tuple[tuple[str, LegacyAccessPurpose], ...] = (
                (owner, "owner_preview"),
                (grantee, "grantee_session"),
            )
            for actor_account_id, purpose in denied_requests:
                with pytest.raises(LegacyAccessDeniedError):
                    await registry.resolve_access(
                        actor_account_id=actor_account_id,
                        grant_id=denied_grant.grant_id,
                        purpose=purpose,
                        now=denied_at,
                    )
            shell_id = (
                expired_access.shell_id
                if denied_grant.grant_id == expired.grant_id
                else revoked_access.shell_id
            )
            assert shell_id is not None
            for actor_account_id in (owner, grantee):
                with pytest.raises(LegacyAccessDeniedError, match="not active"):
                    await registry.get_shell(
                        actor_account_id=actor_account_id,
                        shell_id=shell_id,
                        now=denied_at,
                    )
            with pytest.raises(LegacyNotFoundError):
                await registry.get_shell(
                    actor_account_id=stranger,
                    shell_id=shell_id,
                    now=denied_at,
                )
            with pytest.raises(LegacyNotFoundError):
                await registry.get_shell_for_grant(
                    actor_account_id=stranger,
                    grant_id=denied_grant.grant_id,
                    now=denied_at,
                )
                with pytest.raises(LegacyAccessDeniedError, match="not active"):
                    await registry.get_shell_for_grant(
                        actor_account_id=actor_account_id,
                        grant_id=denied_grant.grant_id,
                        now=denied_at,
                    )
            with pytest.raises(LegacyAccessDeniedError, match="not active"):
                await registry.update_shell_preferences(
                    actor_account_id=grantee,
                    shell_id=shell_id,
                    preferences=inactive_preferences,
                    expected_shell_revision=1,
                    idempotency_key=f"inactive-preferences-{denied_grant.grant_id}",
                    now=denied_at,
                )

        second_grant = await _issue(
            registry,
            owner_account_id=owner,
            grantee_account_id=second_grantee,
            idempotency_key="second-issue",
        )
        second_grant = await registry.activate(
            actor_account_id=owner,
            grant_id=second_grant.grant_id,
            expected_grant_snapshot_sha256=second_grant.grant_snapshot_sha256,
            idempotency_key="second-activate",
            now=NOW + timedelta(minutes=1),
        )
        second_access = await registry.resolve_access(
            actor_account_id=second_grantee,
            grant_id=second_grant.grant_id,
            purpose="grantee_session",
            now=NOW + timedelta(minutes=2),
        )
        assert second_access.shell_id is not None
        second_turn = await registry.append_shell_turn(
            actor_account_id=second_grantee,
            shell_id=second_access.shell_id,
            actor_role="digital_self",
            actual_heard_text="这是第二份关系壳实际播放的回答。",
            fence=LegacyFence("session-2", "turn-2", "generation-2", 5),
            idempotency_key="second-turn",
            now=NOW + timedelta(minutes=3),
        )
        await registry.update_shell_preferences(
            actor_account_id=second_grantee,
            shell_id=second_access.shell_id,
            preferences=(
                ("preferred_response_length", "detailed"),
                ("question_frequency", "rare"),
            ),
            expected_shell_revision=1,
            idempotency_key="second-preferences",
            now=NOW + timedelta(minutes=4),
        )

        deleted_grant_ids = [
            uuid.UUID(active.grant_id),
            uuid.UUID(expired.grant_id),
            uuid.UUID(revoked.grant_id),
        ]
        deleted_result_ids = deleted_grant_ids + [
            uuid.UUID(shell_id),
            uuid.UUID(turn.shell_turn_id),
        ]
        await registry.delete_for_account(account_id=grantee)
        assert (
            await database_admin.fetchval(
                "SELECT count(*) FROM legacy_grants WHERE grant_id=ANY($1::uuid[])",
                deleted_grant_ids,
            )
            == 0
        )
        assert (
            await database_admin.fetchval(
                "SELECT count(*) FROM legacy_relationship_shells WHERE grant_id=ANY($1::uuid[])",
                deleted_grant_ids,
            )
            == 0
        )
        assert (
            await database_admin.fetchval(
                "SELECT count(*) FROM legacy_shell_turns WHERE grant_id=ANY($1::uuid[])",
                deleted_grant_ids,
            )
            == 0
        )
        assert (
            await database_admin.fetchval(
                "SELECT count(*) FROM legacy_audit_events WHERE grant_id=ANY($1::uuid[])",
                deleted_grant_ids,
            )
            == 0
        )
        assert (
            await database_admin.fetchval(
                "SELECT count(*) FROM legacy_command_receipts WHERE result_id=ANY($1::uuid[])",
                deleted_result_ids,
            )
            == 0
        )
        assert (
            await database_admin.fetchval(
                "SELECT count(*) FROM legacy_grants WHERE grant_id=$1",
                uuid.UUID(second_grant.grant_id),
            )
            == 1
        )
        assert await database_admin.fetchval("SELECT count(*) FROM mock_owner_core") == 3

        await registry.delete_for_account(account_id=owner)
        for table in (
            "legacy_grants",
            "legacy_relationship_shells",
            "legacy_shell_turns",
            "legacy_audit_events",
            "legacy_command_receipts",
        ):
            assert await database_admin.fetchval(f"SELECT count(*) FROM {table}") == 0
        assert (
            await database_admin.fetchval(
                "SELECT count(*) FROM legacy_command_receipts WHERE result_id=ANY($1::uuid[])",
                [
                    uuid.UUID(second_grant.grant_id),
                    uuid.UUID(second_access.shell_id),
                    uuid.UUID(second_turn.shell_turn_id),
                ],
            )
            == 0
        )
        assert await database_admin.fetchval("SELECT count(*) FROM mock_owner_core") == 3
    finally:
        if registry is not None:
            await registry.close()
        if database_admin is not None:
            await database_admin.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{app_role}"')
        await admin.close()
