"""Identity Postgres: schema bootstrap, FORCE-RLS, role boundaries and JSONB constraints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.identity.postgres_store import PostgresIdentityStore
from services.identity.tests.identity_pg_test_support import (
    _bootstrap,
    _drop_database,
    _guardian,
    _service,
    pytestmark,  # noqa: F401 - pytest discovers the module-wide marker
)


async def test_schema_roles_rls_and_version_chain() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        try:
            admin = await asyncpg.connect(dsns["admin"])
            try:
                attrs = await admin.fetch(
                    """
                    SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname IN (
                        'memoria_identity',
                        'memoria_identity_registration',
                        'memoria_identity_outbox',
                        'memoria_identity_migration'
                    )
                    ORDER BY rolname
                    """
                )
                assert len(attrs) == 4
                for row in attrs:
                    assert bool(row["rolcanlogin"])
                    assert not bool(row["rolsuper"])
                    assert not bool(row["rolbypassrls"])
                open_policies = await admin.fetch(
                    """
                    SELECT tablename, policyname, qual, with_check
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename LIKE 'identity\\_%'
                    """
                )
                assert open_policies, "expected identity RLS policies"
                for row in open_policies:
                    assert str(row["qual"] or "").strip().lower() != "true"
                    assert str(row["with_check"] or "").strip().lower() != "true"
                rls = await admin.fetch(
                    """
                    SELECT c.relname AS tablename,
                           c.relrowsecurity,
                           c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relname LIKE 'identity\\_%'
                      AND c.relkind = 'r'
                    """
                )
                assert {str(row["tablename"]) for row in rls} == {
                    "identity_persons",
                    "identity_relationships",
                    "identity_device_bindings",
                    "identity_device_binding_roles",
                    "identity_audit_events",
                    "identity_outbox",
                    "identity_transfer_intents",
                    "identity_idempotency_records",
                    "identity_persona_assignments",
                    "identity_custom_personas",
                    "identity_subject_releases",
                }
                for row in rls:
                    assert bool(row["relrowsecurity"])
                    assert bool(row["relforcerowsecurity"])
                # Dedicated DDL owner owns the objects; runtime roles cannot
                # create schema objects.
                owner_has_create = await admin.fetchval(
                    """
                    SELECT has_schema_privilege(
                        'memoria_identity_owner', 'public', 'CREATE'
                    )
                    """
                )
                assert owner_has_create is True
                for runtime_role in (
                    "memoria_identity",
                    "memoria_identity_registration",
                    "memoria_identity_outbox",
                    "memoria_identity_migration",
                ):
                    runtime_create = await admin.fetchval(
                        """
                        SELECT has_schema_privilege($1, 'public', 'CREATE')
                        """,
                        runtime_role,
                    )
                    assert runtime_create is False
                table_owners = await admin.fetch(
                    """
                    SELECT c.relname, pg_get_userbyid(c.relowner) AS owner
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relname LIKE 'identity\\_%'
                      AND c.relkind = 'r'
                    """
                )
                assert {str(row["owner"]) for row in table_owners} == {
                    "memoria_identity_owner"
                }
                definer_ports = await admin.fetch(
                    """
                    SELECT p.proname,
                           pg_get_userbyid(p.proowner) AS owner,
                           p.prosecdef,
                           p.proconfig
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                      AND p.proname IN (
                        'identity_person_visible',
                        'identity_relationship_visible',
                        'identity_binding_visible',
                        'identity_audit_visible',
                        'identity_idempotency_visible',
                        'identity_register_person',
                        'identity_write_idempotency',
                        'identity_patch_idempotency_result',
                        'identity_self_update_profile',
                        'identity_declare_age_evidence',
                        'identity_verify_age_evidence',
                        'identity_next_binding_version'
                      )
                    """
                )
                assert len(definer_ports) == 12
                for row in definer_ports:
                    assert str(row["owner"]) == "memoria_identity_owner"
                    assert bool(row["prosecdef"])
                    config = row["proconfig"] or []
                    assert "row_security=on" in config
                # The API role must not hold direct write privileges on the
                # authoritative tables (a GUC is not a credential), and the
                # registration port is EXECUTE-only for the dedicated
                # registration role.
                for table, privilege in (
                    ("identity_persons", "INSERT"),
                    ("identity_audit_events", "INSERT"),
                    ("identity_audit_events", "UPDATE"),
                    ("identity_outbox", "INSERT"),
                    ("identity_outbox", "UPDATE"),
                    ("identity_idempotency_records", "INSERT"),
                    ("identity_idempotency_records", "UPDATE"),
                ):
                    granted = await admin.fetchval(
                        """
                        SELECT has_table_privilege(
                            'memoria_identity', $1::regclass, $2
                        )
                        """,
                        table,
                        privilege,
                    )
                    assert granted is False, f"{table} {privilege} must be revoked"
                api_register_exec = await admin.fetchval(
                    """
                    SELECT has_function_privilege(
                        'memoria_identity',
                        to_regprocedure(
                            'identity_register_person(text,text,text,text,text,'
                            'text,text,text,timestamptz,timestamptz,text,jsonb,'
                            'text)'
                        ),
                        'EXECUTE'
                    )
                    """
                )
                assert api_register_exec is False
                registration_register_exec = await admin.fetchval(
                    """
                    SELECT has_function_privilege(
                        'memoria_identity_registration',
                        to_regprocedure(
                            'identity_register_person(text,text,text,text,text,'
                            'text,text,text,timestamptz,timestamptz,text,jsonb,'
                            'text)'
                        ),
                        'EXECUTE'
                    )
                    """
                )
                assert registration_register_exec is True
                # The standalone unrestricted registration snapshot port must
                # not exist: replay only happens through the compare-or-insert
                # register port.
                snapshot_gone = await admin.fetchval(
                    """
                    SELECT to_regprocedure(
                        'identity_person_registration_snapshot(text)'
                    ) IS NULL
                    """
                )
                assert snapshot_gone is True
                for port, signature in (
                    (
                        "identity_write_audit",
                        "identity_write_audit(text,text,text,text,text,text,"
                        "text,text,jsonb,timestamptz)",
                    ),
                    (
                        "identity_enqueue_outbox",
                        "identity_enqueue_outbox(text,text,text,jsonb,"
                        "timestamptz)",
                    ),
                    (
                        "identity_update_person",
                        "identity_update_person(text,text,text,text,text,text,"
                        "text,text,timestamptz,text)",
                    ),
                ):
                    gone = await admin.fetchval(
                        """
                        SELECT to_regprocedure($1) IS NULL
                        """,
                        signature,
                    )
                    assert gone is True, f"generic port {port} must be removed"
                for port, signature in (
                    (
                        "identity_next_binding_version",
                        "identity_next_binding_version(text)",
                    ),
                    (
                        "identity_write_idempotency",
                        "identity_write_idempotency(text,text,text,text,jsonb,"
                        "timestamptz)",
                    ),
                    (
                        "identity_patch_idempotency_result",
                        "identity_patch_idempotency_result(text,text,jsonb)",
                    ),
                    (
                        "identity_self_update_profile",
                        "identity_self_update_profile(text,text,text,text,"
                        "timestamptz,text)",
                    ),
                    (
                        "identity_declare_age_evidence",
                        "identity_declare_age_evidence(text,text,timestamptz,"
                        "text)",
                    ),
                ):
                    api_exec = await admin.fetchval(
                        """
                        SELECT has_function_privilege(
                            'memoria_identity', to_regprocedure($1), 'EXECUTE'
                        )
                        """,
                        signature,
                    )
                    assert api_exec is True, f"API role must EXECUTE {port}"
                    reg_exec = await admin.fetchval(
                        """
                        SELECT has_function_privilege(
                            'memoria_identity_registration',
                            to_regprocedure($1),
                            'EXECUTE'
                        )
                        """,
                        signature,
                    )
                    assert reg_exec is False, (
                        f"registration role must not EXECUTE {port}"
                    )
            finally:
                await admin.close()

            now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
            parent = (
                await service.register_person(
                    display_name="家长",
                    timezone="Asia/Shanghai",
                    subject_category="adult",
                    age_band="adult",
                    age_evidence_status="verified",
                    age_evidence_id="evidence-pg-adult",
                    now=now,
                )
            ).person_id
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
            await _guardian(service, guardian=parent, ward=child, now=now)
            v1 = await service.create_binding(
                device_id="dev-pg-a",
                declared_mode="parent_for_child",
                account_owner_person_id=parent,
                primary_subject_ids=(child,),
                roles=((parent, "guardian"),),
                service_profile_version="student-cn-v3",
                policy_bundle_version="policy-cn-minor-v5",
                now=now,
            )
            assert v1.binding_version == 1
            v2 = await service.supersede_binding(
                device_id="dev-pg-a",
                declared_mode="parent_for_child",
                primary_subject_ids=(child,),
                roles=((parent, "guardian"),),
                service_profile_version="student-cn-v4",
                policy_bundle_version="policy-cn-minor-v6",
                actor_person_id=parent,
                now=now + timedelta(minutes=1),
            )
            assert v2.binding_version == 2
            assert v2.supersedes_binding_id == v1.binding_id

            # Cross-person read with the explicit actor is fine.
            assert (
                await service.get_person(child, actor_person_id=parent) is not None
            )

            # Cross-family negative: a foreign actor never sees the other
            # family's binding or its members.
            other_parent = (
                await service.register_person(
                    display_name="另一家",
                    timezone="Asia/Shanghai",
                    subject_category="adult",
                    age_band="adult",
                    age_evidence_status="verified",
                    age_evidence_id="evidence-pg-adult",
                    now=now,
                )
            ).person_id
            manifest_b = await service.get_active_manifest(
                "dev-pg-a", now=now, actor_person_id=other_parent
            )
            assert manifest_b is None
            versions_b = await service.list_binding_versions(
                "dev-pg-a", actor_person_id=other_parent
            )
            assert versions_b == ()
            hidden = await store.get_person(parent, actor_person_id=other_parent)
            assert hidden is None
        finally:
            await store.close()
    finally:
        await _drop_database(database)


async def test_api_role_rls_rejects_cross_actor_binding_read_and_write() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="绑定所有者",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-rls-owner",
                now=now,
            )
        ).person_id
        foreign_actor = (
            await service.register_person(
                display_name="其他用户",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-rls-foreign",
                now=now,
            )
        ).person_id
        binding = await service.create_binding(
            device_id=f"rls-device-{uuid.uuid4().hex[:8]}",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            roles=(),
            service_profile_version="profile-rls-v1",
            policy_bundle_version="policy-rls-v1",
            now=now,
        )

        api = await asyncpg.connect(dsns["api"])
        try:
            await api.fetchval(
                "SELECT set_config('app.identity_actor', $1, false)",
                foreign_actor,
            )
            hidden = await api.fetchrow(
                """
                SELECT binding_id
                FROM identity_device_bindings
                WHERE binding_id = $1
                """,
                binding.binding_id,
            )
            assert hidden is None

            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.execute(
                    """
                    INSERT INTO identity_device_bindings (
                        binding_id, device_id, declared_mode,
                        account_owner_person_id, binding_version, status, reason,
                        valid_from, service_profile_version,
                        policy_bundle_version, created_at
                    ) VALUES (
                        $1, $2, 'self_use', $3, 1, 'active', 'create',
                        $4, 'profile-forged', 'policy-forged', $4
                    )
                    """,
                    f"forged-binding-{uuid.uuid4().hex[:8]}",
                    f"forged-device-{uuid.uuid4().hex[:8]}",
                    owner,
                    now,
                )
        finally:
            await api.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_initialize_rejects_schema_without_force_rls() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = PostgresIdentityStore(
        dsns["api"], registration_dsn=dsns["registration"]
    )
    try:
        admin = await asyncpg.connect(dsns["admin"])
        try:
            await admin.execute(
                "ALTER TABLE identity_persons NO FORCE ROW LEVEL SECURITY"
            )
        finally:
            await admin.close()
        with pytest.raises(RuntimeError, match="force row-level security"):
            await store.initialize(expected_role="memoria_identity")
    finally:
        await store.close()
        await _drop_database(database)


async def test_outbox_worker_role_is_minimal() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
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
        await service.create_binding(
            device_id="dev-pg-worker",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
        worker = await asyncpg.connect(dsns["outbox"])
        try:
            rows = await worker.fetch("SELECT topic FROM identity_outbox")
            assert {str(row["topic"]) for row in rows} == {
                "identity.person.registered",
                "identity.binding.created"
            }
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await worker.fetch("SELECT person_id FROM identity_persons")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await worker.fetch(
                    "SELECT binding_id FROM identity_device_bindings"
                )
        finally:
            await worker.close()
    finally:
        await store.close()
        await _drop_database(database)


async def test_jsonb_constraints_reject_non_array_permissions() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
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
        manifest = await service.create_binding(
            device_id="dev-pg-jsonb",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
        admin = await asyncpg.connect(dsns["admin"])
        try:
            # Existing role grants are immutable: any permissions rewrite is
            # refused by the immutable trigger before the JSONB CHECK runs.
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await admin.execute(
                    """
                    INSERT INTO identity_device_binding_roles (
                        binding_id, person_id, role, status,
                        permissions_json, granted_at
                    ) VALUES ($1, $2, 'member', 'active',
                              '{"not": "an array"}'::jsonb, now())
                    """,
                    manifest.binding_id,
                    owner,
                )
            with pytest.raises(asyncpg.exceptions.RaiseError):
                await admin.execute(
                    """
                    UPDATE identity_device_binding_roles
                    SET permissions_json = '["device.status.view"]'::jsonb
                    WHERE binding_id = $1 AND role = 'account_owner'
                    """,
                    manifest.binding_id,
                )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await admin.execute(
                    """
                    INSERT INTO identity_audit_events (
                        event_id, action, payload_json, created_at
                    ) VALUES ('audit-bad-json', 'x', '[1,2]'::jsonb, now())
                    """
                )
        finally:
            await admin.close()
    finally:
        await store.close()
        await _drop_database(database)
