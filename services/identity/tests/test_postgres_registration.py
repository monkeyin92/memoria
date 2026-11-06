"""Identity Postgres: registration, binding versions, compare-or-insert and age authority."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.identity.domain import (
    AgeEvidenceError,
    IdentityConflictError,
    IdentityNotFoundError,
)
from services.identity.tests.identity_pg_test_support import (
    _bootstrap,
    _drop_database,
    _guardian,
    _service,
    pytestmark,  # noqa: F401 - pytest discovers the module-wide marker
)


async def test_account_registration_reconciliation_transaction_and_role_boundary() -> None:
    database = f"memoria_reconciliation_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _authority = await _service(dsns)
        now = datetime.now(UTC)
        evidence = "control-wechat-phone-v1:" + "a" * 64
        person = await service.register_person(
            person_id="legacy-owner", actor_person_id="legacy-owner",
            display_name="主人", timezone="Asia/Shanghai", now=now,
        )
        # Concurrent login/binding retries may both observe unknown. The DB
        # row lock guarantees one mutation and one audit/outbox pair.
        results = await asyncio.gather(*(service.reconcile_account_registration(
            person_id=person.person_id, evidence_id=evidence, source_revision=1, now=now,
        ) for _ in range(2)))
        assert results[0] == results[1]
        assert results[0].subject_category == "adult"
        assert results[0].created_at == person.created_at
        admin = await asyncpg.connect(dsns["admin"])
        registration = await asyncpg.connect(dsns["registration"])
        api = await asyncpg.connect(dsns["api"])
        query = "SELECT identity_reconcile_account_registration($1,$2,$3,$4,$5)"
        try:
            audits = await admin.fetch(
                "SELECT * FROM identity_audit_events WHERE action = 'person.registration_reconciled'",
            )
            assert len(audits) == 1
            payload = json.loads(audits[0]["payload_json"])
            assert payload["source"] == "control.wechat_phone_registration.v1"
            assert payload["source_profile_revision"] == 1
            assert payload["evidence_id"] == evidence
            assert "verifier_person_id" not in payload
            assert await admin.fetchval(
                "SELECT count(*) FROM identity_outbox WHERE event_id = $1", audits[0]["event_id"],
            ) == 1
            async with api.transaction():
                await api.execute("SELECT set_config('app.identity_scope','registration',true)")
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await api.execute(query, person.person_id, evidence, 1, now, now + timedelta(seconds=1))
            # A failed transaction cannot leave a green person without its
            # audit/outbox, and stale snapshots cannot overwrite a new fact.
            untouched = await service.register_person(
                person_id="retry-owner", display_name="待同步", timezone="Asia/Shanghai", now=now,
            )
            with pytest.raises(RuntimeError, match="rollback"):
                async with registration.transaction():
                    await registration.execute(query, untouched.person_id, evidence, 1, now, now + timedelta(seconds=1))
                    raise RuntimeError("rollback")
            assert await service.get_person(untouched.person_id, actor_person_id=untouched.person_id) == untouched
            assert await admin.fetchval(
                "SELECT count(*) FROM identity_audit_events WHERE person_id=$1 AND action='person.registration_reconciled'",
                untouched.person_id,
            ) == 0
            with pytest.raises(asyncpg.PostgresError) as conflict:
                await registration.execute(query, untouched.person_id, evidence, 1, now - timedelta(seconds=1), now + timedelta(seconds=1))
            assert conflict.value.sqlstate == "II001"
            for bad_evidence, revision in [("self-verified", 1), (evidence, 0)]:
                with pytest.raises(asyncpg.PostgresError):
                    await registration.execute(query, untouched.person_id, bad_evidence, revision, now, now + timedelta(seconds=1))
            for category, band, status, person_status in (
                ("minor", "under_14", "unverified", "active"),
                ("unknown", "unknown", "disputed", "active"),
                ("unknown", "unknown", "unverified", "disabled"),
            ):
                await admin.execute(
                    "UPDATE identity_persons SET subject_category=$2,age_band=$3,age_evidence_status=$4,status=$5 WHERE person_id=$1",
                    untouched.person_id, category, band, status, person_status,
                )
                with pytest.raises(asyncpg.PostgresError) as protected:
                    await registration.execute(query, untouched.person_id, evidence, 1, now, now + timedelta(seconds=1))
                assert protected.value.sqlstate == "II001"
                with pytest.raises(IdentityConflictError):
                    await service.reconcile_account_registration(
                        person_id=untouched.person_id, evidence_id=evidence, source_revision=1, now=now,
                    )
        finally:
            await admin.close()
            await registration.close()
            await api.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_concurrent_member_appends_never_overwrite_on_real_pg() -> None:
    """Two appends derived from version 1: one wins, one conflicts, no loss.

    Real PostgreSQL plus the per-device advisory lock: the compare-and-set
    expectation rejects the stale derivation instead of letting the second
    writer replace the first member with its own list.  Retrying with the
    winner's binding id appends both members, which is what the Control API
    member route does inside its bounded retry.
    """
    database = f"memoria_members_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="家长",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-owner",
                now=now,
            )
        ).person_id
        first = (
            await service.register_person(
                display_name="老大",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="under_14",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        second = (
            await service.register_person(
                display_name="老二",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="14_17",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        v1 = await service.create_binding(
            device_id="dev-pg-members",
            declared_mode="family_shared",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            family_space_id="family-pg-members",
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
            now=now,
        )

        async def append(subject_id: str, minutes: int):
            return await service.supersede_binding(
                device_id="dev-pg-members",
                declared_mode="family_shared",
                primary_subject_ids=(owner, subject_id),
                family_space_id="family-pg-members",
                service_profile_version="family-v1",
                policy_bundle_version="policy-family-v1",
                actor_person_id=owner,
                now=now + timedelta(minutes=minutes),
                expected_binding_id=v1.binding_id,
            )

        results = await asyncio.gather(
            append(first, 1), append(second, 2), return_exceptions=True
        )
        wins = [item for item in results if not isinstance(item, BaseException)]
        losses = [item for item in results if isinstance(item, BaseException)]
        assert len(wins) == 1, results
        assert len(losses) == 1, results
        assert isinstance(losses[0], IdentityConflictError)
        winner = wins[0]
        assert winner.binding_version == 2
        active = await service.get_active_manifest(
            "dev-pg-members", now=now + timedelta(minutes=3), actor_person_id=owner
        )
        assert active is not None
        assert active.binding_version == 2
        assert len(active.primary_subject_ids) == 2
        added = set(active.primary_subject_ids) - {owner}
        assert added in ({first}, {second}), added
        versions = await service.list_binding_versions(
            "dev-pg-members", actor_person_id=owner
        )
        assert [item.binding_version for item in versions] == [1, 2]
        # The rejected attempt leaves no audit row: only the winner advanced
        # the version, so nothing downstream can read the stale derivation as
        # an accepted authorization.
        admin = await asyncpg.connect(dsns["admin"])
        try:
            supersedes = await admin.fetchval(
                "SELECT count(*) FROM identity_audit_events "
                "WHERE action = 'binding.supersede'"
            )
            assert supersedes == 1
        finally:
            await admin.close()

        retried = await service.supersede_binding(
            device_id="dev-pg-members",
            declared_mode="family_shared",
            primary_subject_ids=(*active.primary_subject_ids, first if added == {second} else second),
            family_space_id="family-pg-members",
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
            actor_person_id=owner,
            now=now + timedelta(minutes=4),
            expected_binding_id=active.binding_id,
        )
        assert retried.binding_version == 3
        assert set(retried.primary_subject_ids) == {owner, first, second}
        assert retried.family_space_id == "family-pg-members"
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


@pytest.mark.asyncio
async def test_owner_reads_superseded_versions_with_their_rosters() -> None:
    """An owner who is not a primary subject still sees each old version whole.

    Superseding marks the old version's role rows ``superseded``. The owner
    can read every version of their binding, so they must also read who was
    on it; otherwise the old version decodes without a primary subject and
    the version history fails. A stranger still sees none of it.
    """
    database = f"memoria_roster_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)

        async def person(name: str, category: str, band: str) -> str:
            return (
                await service.register_person(
                    display_name=name,
                    timezone="Asia/Shanghai",
                    subject_category=category,
                    age_band=band,
                    age_evidence_status="verified" if category == "adult" else "unverified",
                    age_evidence_id="evidence-" + name if category == "adult" else None,
                    now=now,
                )
            ).person_id

        owner = await person("家长", "adult", "adult")
        stranger = await person("路人", "adult", "adult")
        child = await person("老大", "minor", "under_14")
        sibling = await person("老二", "minor", "14_17")
        v1 = await service.create_binding(
            device_id="dev-pg-roster",
            declared_mode="family_shared",
            account_owner_person_id=owner,
            primary_subject_ids=(child,),
            family_space_id="family-pg-roster",
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
            now=now,
        )
        await service.supersede_binding(
            device_id="dev-pg-roster",
            declared_mode="family_shared",
            primary_subject_ids=(child, sibling),
            family_space_id="family-pg-roster",
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
            actor_person_id=owner,
            now=now + timedelta(minutes=1),
            expected_binding_id=v1.binding_id,
        )
        versions = await service.list_binding_versions(
            "dev-pg-roster", actor_person_id=owner
        )
        assert [item.binding_version for item in versions] == [1, 2]
        assert versions[0].primary_subject_ids == (child,)
        assert set(versions[1].primary_subject_ids) == {child, sibling}
        assert (
            await service.list_binding_versions("dev-pg-roster", actor_person_id=stranger)
            == ()
        )
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


@pytest.mark.asyncio
async def test_new_owner_binding_version_allocation_sees_revoked_history() -> None:
    """Version allocation must not be narrowed by the new owner's RLS view."""
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
        first_owner = (
            await service.register_person(
                display_name="原所有者",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-first-owner",
                now=now,
            )
        ).person_id
        second_owner = (
            await service.register_person(
                display_name="新所有者",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-second-owner",
                now=now,
            )
        ).person_id
        first = await service.create_binding(
            device_id="dev-pg-rebind",
            declared_mode="self_use",
            account_owner_person_id=first_owner,
            primary_subject_ids=(first_owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
        await service.revoke_binding(
            device_id="dev-pg-rebind",
            actor_person_id=first_owner,
            now=now + timedelta(minutes=1),
        )

        rebound = await service.create_binding(
            device_id="dev-pg-rebind",
            declared_mode="self_use",
            account_owner_person_id=second_owner,
            primary_subject_ids=(second_owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now + timedelta(minutes=2),
        )
        assert first.binding_version == 1
        assert rebound.binding_version == 2
        admin = await asyncpg.connect(dsns["admin"])
        try:
            versions = await admin.fetch(
                """
                SELECT binding_version, status, account_owner_person_id
                FROM identity_device_bindings
                WHERE device_id = $1
                ORDER BY binding_version
                """,
                "dev-pg-rebind",
            )
            assert [(row["binding_version"], row["status"]) for row in versions] == [
                (1, "revoked"),
                (2, "active"),
            ]
            assert versions[1]["account_owner_person_id"] == second_owner
        finally:
            await admin.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


@pytest.mark.asyncio
async def test_list_active_bindings_for_person_respects_force_rls() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="RLS列表主人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-list-owner",
                now=now,
            )
        ).person_id
        child = (
            await service.register_person(
                display_name="RLS列表孩子",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="under_14",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        member = (
            await service.register_person(
                display_name="RLS列表成员",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-list-member",
                now=now,
            )
        ).person_id
        foreign_actor = (
            await service.register_person(
                display_name="RLS外部用户",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-list-foreign",
                now=now,
            )
        ).person_id
        await _guardian(service, guardian=owner, ward=child, now=now)
        family = await service.propose_relationship(
            source_person_id=member,
            target_person_id=child,
            relation_type="family_member_of",
            established_evidence_id="evidence-pg-list-family",
            actor_person_id=member,
            now=now,
        )
        for person_id in (member, child):
            await service.confirm_relationship(
                relationship_id=family.relationship_id,
                person_id=person_id,
                now=now,
            )
        manifest = await service.create_binding(
            device_id=f"dev-pg-list-{uuid.uuid4().hex[:8]}",
            declared_mode="family_shared",
            account_owner_person_id=owner,
            primary_subject_ids=(child,),
            roles=((member, "member"),),
            family_space_id=f"family-pg-list-{uuid.uuid4().hex[:8]}",
            service_profile_version="family-v1",
            policy_bundle_version="policy-family-v1",
            now=now,
        )

        owner_visible = await store.list_active_bindings_for_person(
            owner, now=now, actor_person_id=owner
        )
        member_visible = await store.list_active_bindings_for_person(
            member, now=now, actor_person_id=member
        )
        foreign_visible = await store.list_active_bindings_for_person(
            member, now=now, actor_person_id=foreign_actor
        )
        foreign_own = await service.list_active_manifests_for_person(
            foreign_actor, now=now, actor_person_id=foreign_actor
        )

        assert [binding.binding_id for binding in owner_visible] == [
            manifest.binding_id
        ]
        assert [binding.binding_id for binding in member_visible] == [
            manifest.binding_id
        ]
        assert foreign_visible == ()
        assert foreign_own == ()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_registration_authority_role_gated_and_guc_not_a_credential() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        # 1) A raw SQL client holding the API DSN cannot forge the
        # registration/internal GUC to create persons or write audit, outbox
        # or idempotency rows: the API role simply has no INSERT privilege.
        api = await asyncpg.connect(dsns["api"])
        try:
            for scope in ("registration", "internal"):
                await api.execute(f"SET app.identity_scope = '{scope}'")
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(
                        """
                        INSERT INTO identity_persons (
                            person_id, display_name, subject_category, age_band,
                            age_evidence_status, locale, timezone, status,
                            created_at, updated_at
                        ) VALUES (
                            $1, '伪造注册', 'unknown', 'unknown', 'unverified',
                            'zh-CN', 'Asia/Shanghai', 'active', now(), now()
                        )
                        """,
                        f"forged-{scope}",
                    )
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(
                        """
                        INSERT INTO identity_audit_events (
                            event_id, action, payload_json, created_at
                        ) VALUES ($1, 'person.register', '{}'::jsonb, now())
                        """,
                        f"forged-audit-{scope}",
                    )
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(
                        """
                        INSERT INTO identity_outbox (
                            outbox_id, event_id, topic, payload_json, status,
                            attempts, locked_until, last_error_code, created_at,
                            delivered_at, updated_at
                        ) VALUES (
                            $1, $2, 'identity.binding.created', '{}'::jsonb,
                            'pending', 0, NULL, NULL, now(), NULL, now()
                        )
                        """,
                        f"forged-outbox-{scope}",
                        f"forged-evt-{scope}",
                    )
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(
                        """
                        INSERT INTO identity_idempotency_records (
                            scope_key, idempotency_key, operation, content_hash,
                            result_payload_json, created_at
                        ) VALUES (
                            $1, $2, 'transfer.create', $3, '{}'::jsonb, now()
                        )
                        """,
                        f"forged-{scope}:transfer:x",
                        "forged-key",
                        "a" * 64,
                    )
            # Forging the actor GUC does not unlock person creation either.
            await api.execute("SET app.identity_scope = 'registration'")
            await api.execute("SET app.identity_actor = 'somebody-else'")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.execute(
                    """
                    INSERT INTO identity_persons (
                        person_id, display_name, subject_category, age_band,
                        age_evidence_status, locale, timezone, status,
                        created_at, updated_at
                    ) VALUES (
                        $1, '伪造actor', 'unknown', 'unknown', 'unverified',
                        'zh-CN', 'Asia/Shanghai', 'active', now(), now()
                    )
                    """,
                    "forged-actor",
                )
        finally:
            await api.close()

        # 2) The dedicated registration path succeeds and persists person +
        # audit + outbox in one transaction.
        owner = (
            await service.register_person(
                display_name="本人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-adult",
                now=now,
            )
        ).person_id
        admin = await asyncpg.connect(dsns["admin"])
        try:
            person = await admin.fetchrow(
                "SELECT person_id FROM identity_persons WHERE person_id = $1",
                owner,
            )
            assert person is not None
            audit = await admin.fetchrow(
                """
                SELECT event_id, action FROM identity_audit_events
                WHERE person_id = $1
                """,
                owner,
            )
            assert audit is not None
            assert str(audit["action"]) == "person.register"
            outbox = await admin.fetchrow(
                """
                SELECT outbox_id FROM identity_outbox
                WHERE topic = 'identity.person.registered'
                  AND payload_json->>'person_id' = $1
                """,
                owner,
            )
            assert outbox is not None
        finally:
            await admin.close()

        # 3) The registration role holds no table privileges: it can neither
        # read relationships/bindings/transfers nor update other persons.
        reg = await asyncpg.connect(dsns["registration"])
        try:
            for statement in (
                "SELECT person_id FROM identity_persons",
                "SELECT relationship_id FROM identity_relationships",
                "SELECT binding_id FROM identity_device_bindings",
                "SELECT transfer_id FROM identity_transfer_intents",
                "SELECT event_id FROM identity_audit_events",
            ):
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await reg.fetch(statement)
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await reg.execute(
                    """
                    UPDATE identity_persons
                    SET display_name = '篡改'
                    WHERE person_id = $1
                    """,
                    owner,
                )
        finally:
            await reg.close()

        # 4) Cross-actor age DECLARATION changes (guardian -> child) succeed
        # through the action-level port: a guardian maintains the child's
        # minor band, never adult/verified status.
        child = (
            await service.register_person(
                display_name="孩子",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="under_14",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        await _guardian(service, guardian=owner, ward=child, now=now)
        grown = await service.declare_age_evidence(
            person_id=child,
            age_band="14_17",
            actor_person_id=owner,
            now=now + timedelta(days=1),
        )
        assert grown.subject_category == "minor"
        assert grown.age_band == "14_17"
    finally:
        await store.close()
        await _drop_database(database)


async def test_register_compare_or_insert_replay_drift_and_concurrency() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        # 1) Exact replay returns the persisted DB row (never the unpersisted
        #    request snapshot) and writes no duplicate trail rows.
        first = await service.register_person(
            display_name="幂等",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            now=now,
        )
        replayed = await service.register_person(
            display_name="幂等",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            person_id=first.person_id,
            now=now + timedelta(minutes=5),
        )
        admin = await asyncpg.connect(dsns["admin"])
        try:
            row = await admin.fetchrow(
                "SELECT * FROM identity_persons WHERE person_id = $1",
                first.person_id,
            )
            assert row is not None
            assert replayed.display_name == str(row["display_name"])
            assert replayed.subject_category == str(row["subject_category"])
            assert replayed.age_band == str(row["age_band"])
            assert replayed.locale == str(row["locale"])
            assert replayed.timezone == str(row["timezone"])
            person_count = await admin.fetchval(
                "SELECT count(*) FROM identity_persons WHERE person_id = $1",
                first.person_id,
            )
            assert person_count == 1
            audit_count = await admin.fetchval(
                "SELECT count(*) FROM identity_audit_events "
                "WHERE person_id = $1",
                first.person_id,
            )
            assert audit_count == 1
            outbox_count = await admin.fetchval(
                """
                SELECT count(*) FROM identity_outbox
                WHERE payload_json->>'person_id' = $1
                """,
                first.person_id,
            )
            assert outbox_count == 1
        finally:
            await admin.close()

        # 2) Content drift is an explicit conflict.
        with pytest.raises(IdentityConflictError):
            await service.register_person(
                display_name="改名",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=first.person_id,
                now=now,
            )

        # 3) The registration role cannot read another person without proving
        #    the full canonical content: wrong/partial fields raise and the
        #    error reveals no row content.
        canonical_payload = json.dumps(
            {
                "person_id": first.person_id,
                "display_name": "改名",
                "subject_category": "unknown",
                "age_band": "unknown",
                "age_evidence_status": "unverified",
                "locale": "zh-CN",
                "timezone": "Asia/Shanghai",
                "status": "active",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
            ensure_ascii=False,
        )
        reg = await asyncpg.connect(dsns["registration"])
        try:
            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await reg.fetchval(
                    """
                    SELECT identity_register_person(
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13
                    )
                    """,
                    first.person_id,
                    "改名",
                    "unknown",
                    "unknown",
                    "unverified",
                    "zh-CN",
                    "Asia/Shanghai",
                    "active",
                    now,
                    now,
                    None,
                    canonical_payload,
                    None,
                )
            message = str(excinfo.value)
            assert "different authoritative fields" in message
            assert "幂等" not in message
            with pytest.raises(asyncpg.PostgresError):
                await reg.fetchval(
                    """
                    SELECT identity_register_person(
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NULL,
                        NULL
                    )
                    """,
                    first.person_id,
                    "幂等",
                    "unknown",
                    "unknown",
                    "unverified",
                    "zh-CN",
                    "Asia/Shanghai",
                    "active",
                    now,
                    now,
                    None,
                )
        finally:
            await reg.close()

        # 4) Concurrency: exact replays both return the persisted row; a
        #    drift race yields exactly one winner and one conflict.
        same_id = f"conc-same-{uuid.uuid4().hex[:8]}"
        winner_a, winner_b = await asyncio.gather(
            service.register_person(
                display_name="并发",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=same_id,
                now=now,
            ),
            service.register_person(
                display_name="并发",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=same_id,
                now=now,
            ),
        )
        assert winner_a.person_id == winner_b.person_id == same_id
        assert winner_a.display_name == winner_b.display_name == "并发"
        admin = await asyncpg.connect(dsns["admin"])
        try:
            row = await admin.fetchrow(
                "SELECT * FROM identity_persons WHERE person_id = $1",
                same_id,
            )
            assert row is not None
            assert winner_a.display_name == str(row["display_name"])
            assert winner_b.display_name == str(row["display_name"])
        finally:
            await admin.close()
        drift_id = f"conc-drift-{uuid.uuid4().hex[:8]}"
        drift_results = await asyncio.gather(
            service.register_person(
                display_name="甲",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=drift_id,
                now=now,
            ),
            service.register_person(
                display_name="乙",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=drift_id,
                now=now,
            ),
            return_exceptions=True,
        )
        assert sorted(type(result).__name__ for result in drift_results) == [
            "IdentityConflictError",
            "PersonSubject",
        ]
    finally:
        await store.close()
        await _drop_database(database)


async def test_age_authority_raw_update_blocked_and_verification_action_level() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        # 1) The API role cannot rewrite age/status/profile fields with raw
        #    UPDATE even after forging the actor GUC (UPDATE grant revoked).
        victim = (
            await service.register_person(
                display_name="受害者",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        api = await asyncpg.connect(dsns["api"])
        try:
            await api.execute(
                "SELECT set_config('app.identity_actor', $1, false)", victim
            )
            for statement in (
                """
                UPDATE identity_persons
                SET subject_category = 'adult',
                    age_band = 'adult',
                    age_evidence_status = 'verified'
                WHERE person_id = $1
                """,
                """
                UPDATE identity_persons
                SET status = 'disabled'
                WHERE person_id = $1
                """,
                """
                UPDATE identity_persons
                SET display_name = '改名'
                WHERE person_id = $1
                """,
            ):
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(statement, victim)
            # The action-level self port cannot claim adult either.
            with pytest.raises(asyncpg.PostgresError):
                await api.execute(
                    """
                    SELECT identity_declare_age_evidence($1, 'adult', $2, $1)
                    """,
                    victim,
                    now + timedelta(days=1),
                )
            # The verification port is not executable by the API role.
            with pytest.raises(asyncpg.PostgresError):
                await api.execute(
                    """
                    SELECT identity_verify_age_evidence(
                        $1, 'adult', 'verified', 'evidence-x', $1, $2
                    )
                    """,
                    victim,
                    now + timedelta(days=1),
                )
        finally:
            await api.close()
        row = await store.get_person(
            victim, actor_person_id=victim
        )
        assert row is not None
        assert row.subject_category == "unknown"
        assert row.age_evidence_status == "unverified"
        assert row.status == "active"
        assert row.display_name == "受害者"

        # 2) Authoritative verification: verifier must be a verified adult
        #    with visibility (active guardian relationship); evidence id is
        #    mandatory and the audit/outbox trail lands in the same
        #    transaction.
        verifier = (
            await service.register_person(
                display_name="核验人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-verifier",
                now=now,
            )
        ).person_id
        target = (
            await service.register_person(
                display_name="待核验",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        # No relationship yet: the verifier cannot see the target (fail
        # closed on PostgreSQL).
        with pytest.raises(IdentityNotFoundError):
            await service.verify_age_evidence(
                person_id=target,
                evidence_id="evidence-verify-1",
                verifier_person_id=verifier,
                now=now + timedelta(days=1),
            )
        await _guardian(service, guardian=verifier, ward=target, now=now)
        verified = await service.verify_age_evidence(
            person_id=target,
            evidence_id="evidence-verify-1",
            verifier_person_id=verifier,
            now=now + timedelta(days=1),
        )
        assert verified.subject_category == "adult"
        assert verified.age_band == "adult"
        assert verified.age_evidence_status == "verified"
        admin = await asyncpg.connect(dsns["admin"])
        try:
            audit = await admin.fetchrow(
                """
                SELECT action, payload_json FROM identity_audit_events
                WHERE action = 'person.age_verification'
                  AND person_id = $1
                """,
                target,
            )
            assert audit is not None
            audit_payload = json.loads(str(audit["payload_json"]))
            assert audit_payload["evidence_id"] == "evidence-verify-1"
            assert audit_payload["verifier_person_id"] == verifier
            outbox = await admin.fetchrow(
                """
                SELECT topic FROM identity_outbox
                WHERE topic = 'identity.person.age_verified'
                  AND payload_json->>'person_id' = $1
                """,
                target,
            )
            assert outbox is not None
        finally:
            await admin.close()

        # 3) A non-verified guardian cannot verify anyone as an adult.
        plain = (
            await service.register_person(
                display_name="普通监护人",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        ward = (
            await service.register_person(
                display_name="被监护",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="under_14",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        await _guardian(service, guardian=plain, ward=ward, now=now)
        with pytest.raises(AgeEvidenceError):
            await service.verify_age_evidence(
                person_id=ward,
                evidence_id="evidence-guardian-forge",
                verifier_person_id=plain,
                now=now + timedelta(days=1),
            )
        # Self-verification is rejected even for a verified adult.
        with pytest.raises(AgeEvidenceError):
            await service.verify_age_evidence(
                person_id=verifier,
                evidence_id="evidence-self",
                verifier_person_id=verifier,
                now=now + timedelta(days=1),
            )
    finally:
        await store.close()
        await _drop_database(database)
